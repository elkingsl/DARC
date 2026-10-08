#!/usr/bin/env python3
"""DARC Dynamic Island: a small GTK3 overlay that talks to the daemon.

Runs on the SYSTEM Python (needs python-gobject + gtk-layer-shell).
States: OFFLINE / IDLE / LISTENING / PROCESSING / RESPONDING.
Toggle the input box with:  python darcctl.py toggle   (bind it to a key)
"""
import os
import sys

# gtk-layer-shell must be loaded before libwayland-client: preload and re-exec.
_LIB = next((p for p in ("/usr/lib/libgtk-layer-shell.so",
                         "/usr/lib/libgtk-layer-shell.so.0")
             if os.path.exists(p)), None)
if _LIB and _LIB not in os.environ.get("LD_PRELOAD", ""):
    os.environ["LD_PRELOAD"] = f"{_LIB} {os.environ.get('LD_PRELOAD', '')}".strip()
    os.execv(sys.executable, [sys.executable] + sys.argv)

import argparse
import atexit
import json
import math
import signal
import socket
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_foreign("cairo")  # the waveform widget draws with pycairo
except ImportError:
    sys.exit("UI needs pycairo on the system Python: sudo pacman -S python-cairo")
try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell
    HAVE_LAYER = True
except (ValueError, ImportError):
    HAVE_LAYER = False
from gi.repository import Gdk, GLib, Gtk, Pango

try:  # newer GLib moved unix signal sources to GLibUnix
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
except (ValueError, ImportError):
    GLibUnix = None


def unix_signal(sig, callback):
    """Prefer GLibUnix.signal_add; fall back for GLib versions without it."""
    add = getattr(GLibUnix, "signal_add", None) if GLibUnix else None
    if add is None:
        add = GLib.unix_signal_add
    add(GLib.PRIORITY_DEFAULT, sig, callback)


HERE = Path(__file__).resolve().parent


def default_socket_path() -> str:  # keep in sync with daemon/server.py
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(rt, "darc.sock") if rt else f"/tmp/darc-{os.getuid()}.sock"


# state -> (status text, activity animation)
STATES = {
    "OFFLINE":    ("offline", "none"),
    "STARTING":   ("starting", "none"),
    "IDLE":       ("", "none"),
    "LISTENING":  ("listening", "listen"),
    "PROCESSING": ("thinking", "think"),
    "RESPONDING": ("responding", "speak"),
}


class Client(threading.Thread):
    """Reads daemon events on a background thread; auto-reconnects."""

    def __init__(self, path, on_event, on_status):
        super().__init__(daemon=True)
        self.path, self.on_event, self.on_status = path, on_event, on_status
        self.sock = None

    def run(self):
        while True:
            s = socket.socket(socket.AF_UNIX)
            try:
                s.connect(self.path)
            except OSError:
                s.close()
                GLib.idle_add(self.on_status, False)
                time.sleep(2)
                continue
            self.sock = s
            GLib.idle_add(self.on_status, True)
            buf = b""
            try:
                while True:
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            GLib.idle_add(self.on_event, json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
            finally:
                self.sock = None
                s.close()
                GLib.idle_add(self.on_status, False)
            time.sleep(1)

    def send(self, obj) -> bool:
        s = self.sock
        if not s:
            return False
        try:
            s.sendall(json.dumps(obj).encode() + b"\n")
            return True
        except OSError:
            return False


class Activity(Gtk.DrawingArea):
    """Waveform (listening / speaking) or pulsing dots (thinking)."""
    BLUE, PURPLE = (0.35, 0.59, 1.0), (0.72, 0.45, 1.0)

    def __init__(self):
        super().__init__()
        self.set_size_request(64, 20)
        self.set_valign(Gtk.Align.CENTER)
        self.mode, self.t, self._src = "none", 0.0, None
        self.connect("draw", self._draw)

    def set_mode(self, mode):
        self.mode = mode
        if mode == "none":
            if self._src:
                GLib.source_remove(self._src)
                self._src = None
        elif self._src is None:
            self._src = GLib.timeout_add(40, self._tick)
        self.queue_draw()

    def _tick(self):
        self.t += 0.04
        self.queue_draw()
        return True

    def _draw(self, w, cr):
        W, H = w.get_allocated_width(), w.get_allocated_height()
        if self.mode in ("listen", "speak"):
            r, g, b = self.PURPLE if self.mode == "listen" else self.BLUE
            scale = 1.0 if self.mode == "listen" else 0.6
            n = 9
            cr.set_line_width(3)
            cr.set_line_cap(1)  # round
            for i in range(n):
                a = abs(math.sin(self.t * 4 + i * 0.8) * math.cos(self.t * 2.3 + i * 0.37))
                h = max(3, (0.25 + 0.75 * a) * H * scale)
                x = (i + 0.5) * W / n
                cr.set_source_rgb(r, g, b)
                cr.move_to(x, H / 2 - h / 2)
                cr.line_to(x, H / 2 + h / 2)
                cr.stroke()
        elif self.mode == "think":
            r, g, b = self.BLUE
            for k in range(3):
                alpha = 0.3 + 0.7 * max(0.0, math.sin(self.t * 6 - k * 0.9))
                cr.set_source_rgba(r, g, b, alpha)
                cr.arc(W / 2 + (k - 1) * 14, H / 2, 3.5, 0, 2 * math.pi)
                cr.fill()
        return False


class Island(Gtk.Window):
    def __init__(self, sock_path: str, position: str):
        super().__init__()
        self.get_style_context().add_class("darc")
        self.set_title("DARC")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.state = "OFFLINE"
        self.input_open = False
        self._collapse_src = None
        self._setup_layer(position)
        self._load_css()
        self._build()
        self.set_state("OFFLINE")

        self.client = Client(sock_path, self.on_event, self.on_status)
        self.client.start()
        self.connect("destroy", lambda *_: Gtk.main_quit())

    # ---- window plumbing ------------------------------------------------
    def _setup_layer(self, position: str):
        self.layered = False
        try:
            self.layered = HAVE_LAYER and GtkLayerShell.is_supported()
        except Exception:
            pass
        if not self.layered:
            self.set_keep_above(True)  # X11 / no layer-shell: plain window
            return
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "darc")
        v, _, h = position.partition("-")
        edges = {"top": GtkLayerShell.Edge.TOP, "bottom": GtkLayerShell.Edge.BOTTOM,
                 "left": GtkLayerShell.Edge.LEFT, "right": GtkLayerShell.Edge.RIGHT}
        for name in (v, h):  # "center" simply adds no anchor on that axis
            if name in edges:
                GtkLayerShell.set_anchor(self, edges[name], True)
                GtkLayerShell.set_margin(self, edges[name], 12)
        self._keyboard(False)

    def _keyboard(self, on: bool):
        if not self.layered:
            return
        if hasattr(GtkLayerShell, "set_keyboard_mode"):
            mode = GtkLayerShell.KeyboardMode
            GtkLayerShell.set_keyboard_mode(self, mode.EXCLUSIVE if on else mode.NONE)
        else:
            GtkLayerShell.set_keyboard_interactivity(self, on)

    def _load_css(self):
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "styles.css"))
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ---- widgets --------------------------------------------------------
    def _build(self):
        self.island = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.island.get_style_context().add_class("island")
        self.add(self.island)

        header = Gtk.Box(spacing=10)
        dot = Gtk.Box()
        dot.get_style_context().add_class("dot")
        dot.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label="DARC")
        title.get_style_context().add_class("title")
        self.activity = Activity()
        self.status = Gtk.Label()
        self.status.get_style_context().add_class("status")
        for w in (dot, title, self.activity, self.status):
            header.pack_start(w, False, False, 0)
        click = Gtk.EventBox()
        click.add(header)
        click.connect("button-press-event", lambda *_: self.toggle_input())
        self.island.pack_start(click, False, False, 0)

        self.resp = Gtk.Label(xalign=0)
        self.resp.set_line_wrap(True)
        self.resp.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.resp.set_width_chars(40)
        self.resp.set_max_width_chars(48)
        self.resp.get_style_context().add_class("response")
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_max_content_height(220)
        self.scroll.set_min_content_width(328)  # else the label wraps to a sliver
        self.scroll.add(self.resp)
        self.resp_rev = self._revealer(self.scroll)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask DARC...")
        self.entry.get_style_context().add_class("prompt")
        self.entry.connect("activate", self._on_activate)
        self.entry.connect("key-press-event", self._on_key)
        self.entry_rev = self._revealer(self.entry)

        self.island.pack_start(self.resp_rev, False, False, 0)
        self.island.pack_start(self.entry_rev, False, False, 4)

    @staticmethod
    def _revealer(child):
        r = Gtk.Revealer()
        r.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        r.set_transition_duration(180)
        child.show_all()  # children stay 'shown'; the revealer itself toggles
        r.add(child)
        r.set_no_show_all(True)  # a hidden revealer would still reserve width
        r.connect("notify::child-revealed", Island._sync_visible)
        return r

    @staticmethod
    def _sync_visible(r, _pspec=None):
        if not r.get_reveal_child() and not r.get_child_revealed():
            r.hide()

    @staticmethod
    def _reveal(r, on: bool):
        if on:
            r.show()
            r.set_reveal_child(True)
        else:
            r.set_reveal_child(False)
            Island._sync_visible(r)

    # ---- state ----------------------------------------------------------
    def set_state(self, state: str):
        if state not in STATES:
            return
        self.state = state
        ctx = self.island.get_style_context()
        for name in STATES:
            ctx.remove_class(name.lower())
        ctx.add_class(state.lower())
        text, mode = STATES[state]
        self.status.set_text(text)
        self.status.set_visible(bool(text))
        self.activity.set_visible(mode != "none")
        self.activity.set_mode(mode)
        if state == "PROCESSING":  # new request: drop the old answer
            self._cancel_collapse()
            self.resp.set_text("")
            self._reveal(self.resp_rev, False)

    def on_status(self, connected: bool):
        if not connected:
            self.set_state("OFFLINE")
        return False

    def on_event(self, ev: dict):
        kind = ev.get("type")
        if kind in ("hello", "state"):
            self.set_state(ev.get("state", "IDLE"))
        elif kind == "token":
            self._cancel_collapse()
            self.resp.set_text(self.resp.get_text() + ev.get("text", ""))
            self._fit_response()
            self._reveal(self.resp_rev, True)
            GLib.idle_add(self._scroll_bottom)
        elif kind == "done":
            if not self.input_open:
                n = len(ev.get("text", ""))
                self._schedule_collapse(max(6, min(20, n // 40)))
        elif kind == "error":
            self._show_message("\u26a0 " + ev.get("message", "error"), 10)
        return False

    def _fit_response(self):
        """GTK mis-sizes a wrapping label inside a scroller (it measures the
        unwrapped text), so size the scroller from an estimated line count."""
        lines = sum(max(1, math.ceil(len(l) / 36))
                    for l in self.resp.get_text().split("\n"))
        self.scroll.set_min_content_height(min(lines * 19 + 10, 220))

    def _scroll_bottom(self):
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _show_message(self, text: str, seconds: int):
        self.resp.set_text(text)
        self._fit_response()
        self._reveal(self.resp_rev, True)
        if not self.input_open:
            self._schedule_collapse(seconds)

    # ---- collapse / input ------------------------------------------------
    def _cancel_collapse(self):
        if self._collapse_src:
            GLib.source_remove(self._collapse_src)
            self._collapse_src = None

    def _schedule_collapse(self, seconds: int):
        self._cancel_collapse()
        self._collapse_src = GLib.timeout_add_seconds(seconds, self._collapse)

    def _collapse(self):
        self._collapse_src = None
        self.input_open = False
        self._reveal(self.entry_rev, False)
        self._reveal(self.resp_rev, False)
        self._keyboard(False)
        return False

    def _open_input(self):
        self._cancel_collapse()
        self.input_open = True
        self._reveal(self.entry_rev, True)
        if self.resp.get_text():
            self._reveal(self.resp_rev, True)
        self._keyboard(True)
        if not self.layered:
            self.present()
        GLib.idle_add(lambda: self.entry.grab_focus() and False)

    def _close_input(self):
        self.input_open = False
        self._reveal(self.entry_rev, False)
        self._keyboard(False)
        if not self.resp.get_text():
            self._reveal(self.resp_rev, False)

    def toggle_input(self):
        if self.input_open:
            self._close_input()
        else:
            self._open_input()
        return True  # keep the unix-signal source alive

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self._close_input()
            return True
        return False

    def _on_activate(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        entry.set_text("")
        self._close_input()
        if not self.client.send({"type": "submit", "text": text}):
            self._show_message("\u26a0 DARC daemon is not running", 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--position", default="top-right")
    ap.add_argument("--socket", default=default_socket_path())
    args = ap.parse_args()

    island = Island(args.socket, args.position)
    island.show_all()

    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    pidfile = os.path.join(runtime, "darc-ui.pid")
    Path(pidfile).write_text(str(os.getpid()))
    atexit.register(lambda: os.path.exists(pidfile) and os.unlink(pidfile))

    unix_signal(signal.SIGUSR1, island.toggle_input)
    for sig in (signal.SIGINT, signal.SIGTERM):
        unix_signal(sig, Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()