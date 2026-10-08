"""DARC entry point.

  python main.py            daemon + Dynamic Island UI
  python main.py --no-ui    daemon only (UI started separately / systemd)
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
from daemon.server import SocketServer, default_socket_path
from models.llm import LLMError, OllamaLLM

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"


def build_orchestrator(cfg: dict) -> Orchestrator:
    l, d = cfg["llm"], cfg["daemon"]
    llm = OllamaLLM(l["host"], l["model"],
                    keep_alive=l.get("keep_alive", "5m"),
                    options=l.get("options"),
                    timeout=l.get("timeout", 120))
    return Orchestrator(llm, d["system_prompt"], d.get("max_history_turns", 6))


def spawn_ui(cfg: dict, sock: str) -> subprocess.Popen:
    """The UI needs system PyGObject, so it runs on the system Python,
    not inside the uv venv."""
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.Popen(
        [py, str(ROOT / "ui" / "island.py"),
         "--position", cfg.get("ui", {}).get("position", "top-right"),
         "--socket", sock], env=env)


async def run_daemon(cfg: dict, with_ui: bool) -> None:
    orch = build_orchestrator(cfg)
    sock = default_socket_path()
    server = SocketServer(orch, sock)
    try:
        await orch.start()
        await server.start()
    except (LLMError, RuntimeError) as e:
        sys.exit(f"[startup failed] {e}")

    ui = spawn_ui(cfg, sock) if with_ui else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    print(f"DARC daemon up on {sock}  (Ctrl+C to stop)")
    await stop.wait()

    if ui and ui.poll() is None:
        ui.terminate()
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
    ap.add_argument("--cli", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(CONFIG.read_text())
    try:
        asyncio.run(run_cli(cfg) if args.cli
                    else run_daemon(cfg, with_ui=not args.no_ui))
    except KeyboardInterrupt:
        pass
