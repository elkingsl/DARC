"""Launch installed apps by name (from .desktop entries) + Hyprland window ops.

No LLM-written shell here: the app list comes from the system's own .desktop
files and window actions are fixed hyprctl calls."""
import asyncio
import difflib
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass

from daemon.tools.system_cmd import TERMINALS

DESKTOP_DIRS = [
    "/usr/share/applications", "/usr/local/share/applications",
    "~/.local/share/applications", "/var/lib/flatpak/exports/share/applications",
    "~/.local/share/flatpak/exports/share/applications",
]


@dataclass
class App:
    id: str
    name: str
    generic: str
    exec: str
    keywords: str
    terminal: bool


def _parse(path: str) -> App | None:
    fields: dict[str, str] = {}
    in_entry = False
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("["):
                in_entry = line == "[Desktop Entry]"
            elif in_entry and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                fields.setdefault(k.strip(), v.strip())  # first one wins; skips Name[de]
    except OSError:
        return None
    if fields.get("Type") != "Application" or not fields.get("Exec") or not fields.get("Name"):
        return None
    if fields.get("NoDisplay", "").lower() == "true" or fields.get("Hidden", "").lower() == "true":
        return None
    return App(os.path.basename(path)[:-8], fields["Name"], fields.get("GenericName", ""),
               fields["Exec"], fields.get("Keywords", ""),
               fields.get("Terminal", "").lower() == "true")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


class AppLauncher:
    def __init__(self, launcher: str = "hyprctl", terminal: str | None = None,
                 dirs: list[str] | None = None):
        self.launcher, self.terminal = launcher, terminal
        self.dirs = [os.path.expanduser(d) for d in (dirs or DESKTOP_DIRS)]
        self._cache: list[App] = []
        self._loaded = 0.0

    def apps(self) -> list[App]:
        if time.time() - self._loaded > 60:
            seen, found = set(), []
            for d in self.dirs:
                try:
                    names = sorted(os.listdir(d))
                except OSError:
                    continue
                for n in names:
                    if n.endswith(".desktop"):
                        app = _parse(os.path.join(d, n))
                        if app and app.id not in seen:
                            seen.add(app.id)
                            found.append(app)
            self._cache, self._loaded = found, time.time()
        return self._cache

    def resolve(self, query: str) -> App | None:
        q = _norm(re.sub(r"\b(the|app|application|program|up)\b", " ", query.lower()))
        if not q:
            return None
        best, best_score = None, 0.0
        for app in self.apps():
            name, generic = _norm(app.name), _norm(app.generic)
            exe = _norm(os.path.basename(shlex.split(app.exec)[0])) if app.exec else ""
            words = set(name.split()) | set(generic.split())
            if q in (name, exe, _norm(app.id)):
                score = 100.0
            elif name.startswith(q):
                score = 85.0
            elif q in words or q == generic:
                score = 75.0
            elif q in name or q in generic or q in _norm(app.keywords).split():
                score = 65.0
            else:
                score = max(difflib.SequenceMatcher(None, q, name).ratio(),
                            difflib.SequenceMatcher(None, q, exe).ratio()) * 60
            if score > best_score:
                best, best_score = app, score
        return best if best_score >= 55 else None

    async def launch(self, app: App) -> str:
        cmd = re.sub(r"%[a-zA-Z]", "", app.exec.replace("%%", "%")).strip()
        if app.terminal:
            for name in ([self.terminal] if self.terminal else list(TERMINALS)):
                if name and shutil.which(name):
                    cmd = shlex.join([shutil.which(name), *TERMINALS.get(name, ["-e"])]) + " " + cmd
                    break
        if self.launcher == "hyprctl" and shutil.which("hyprctl") \
                and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            proc = await asyncio.create_subprocess_exec(
                "hyprctl", "dispatch", "exec", cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            if proc.returncode == 0 and b"ok" in out.lower():
                return f"Opening {app.name}."
        if shutil.which("gtk-launch"):
            if subprocess.run(["gtk-launch", app.id], capture_output=True).returncode == 0:
                return f"Opening {app.name}."
        subprocess.Popen(shlex.split(cmd), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return f"Opening {app.name}."

    # ---- Hyprland window control -----------------------------------------
    @staticmethod
    def window_args(data: dict) -> list[str] | None:
        op = data.get("op")
        if op == "workspace":
            return ["workspace", str(int(data["n"]))]
        if op == "movetoworkspace":
            return ["movetoworkspace", str(int(data["n"]))]
        if op == "fullscreen":
            return ["fullscreen", "0"]
        if op == "close":
            return ["killactive"]
        return None

    async def window(self, data: dict) -> str:
        args = self.window_args(data)
        if not args or not shutil.which("hyprctl"):
            return "hyprctl isn't available."
        proc = await asyncio.create_subprocess_exec(
            "hyprctl", "dispatch", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        ok = proc.returncode == 0 and b"ok" in out.lower()
        return "Done." if ok else f"hyprctl said: {out.decode(errors='replace').strip()}"
