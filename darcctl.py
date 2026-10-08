#!/usr/bin/env python3
"""Tiny control tool (stdlib only, any Python).

  darcctl.py toggle        open/close the island's input box (bind to a hotkey)
  darcctl.py say "text"    send a prompt and stream the reply in the terminal
  darcctl.py listen        put the UI in LISTENING (waveform) for testing
  darcctl.py cancel        leave LISTENING
  darcctl.py clear         reset conversation context
  darcctl.py state         print the daemon state
"""
import json
import os
import signal
import socket
import sys


def runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/tmp"


def sock_path() -> str:  # keep in sync with daemon/server.py
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(rt, "darc.sock") if rt else f"/tmp/darc-{os.getuid()}.sock"


def connect() -> socket.socket:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(sock_path())
    except OSError:
        sys.exit(f"DARC daemon not running ({sock_path()})")
    return s


def events(s):
    buf = b""
    while True:
        data = s.recv(4096)
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                yield json.loads(line)
            except ValueError:
                pass


def send(s, obj) -> None:
    s.sendall(json.dumps(obj).encode() + b"\n")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "toggle":
        pidfile = os.path.join(runtime_dir(), "darc-ui.pid")
        try:
            os.kill(int(open(pidfile).read()), signal.SIGUSR1)
        except (OSError, ValueError):
            sys.exit("island UI not running")
    elif cmd in ("listen", "cancel", "clear"):
        s = connect()
        send(s, {"type": cmd})
    elif cmd == "state":
        s = connect()
        print(next(events(s))["state"])
    elif cmd == "say" and len(sys.argv) > 2:
        s = connect()
        send(s, {"type": "submit", "text": " ".join(sys.argv[2:])})
        for ev in events(s):
            if ev["type"] == "token":
                print(ev["text"], end="", flush=True)
            elif ev["type"] == "done":
                print()
                break
            elif ev["type"] == "error":
                sys.exit(f"\n[error] {ev['message']}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
