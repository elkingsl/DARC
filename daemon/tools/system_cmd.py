"""Command proposal, safety policy, and execution.

Rules (the spec's "LLM proposes -> validate -> user confirms -> OS executes"):
  * The LLM only PROPOSES one command line (JSON). Nothing runs unconfirmed.
  * A hard deny-list refuses destructive patterns outright.
  * DARC never escalates privileges itself: commands needing root open in a
    terminal where YOU type the password.
  * Everything else runs via bash -c with a timeout, output capped.
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class Proposal:
    command: str
    explanation: str = ""
    needs_terminal: bool = False   # needs root / interactive -> run in a terminal
    warning: str | None = None     # shown in the confirmation
    refused: str | None = None     # hard-denied: never offered for approval
    kind: str = "shell"            # "shell" | "browser" (labels the prompt)


@dataclass
class Result:
    returncode: int
    output: str
    truncated: bool = False
    timed_out: bool = False


PROTECTED = {"/", "/*", "~", "~/", "~/*", "$home", "$home/", "$home/*", ".", "..",
             "*", "/home", "/home/*", "/etc", "/usr", "/boot", "/var", "/bin",
             "/sbin", "/lib", "/lib64", "/opt", "/root", "/dev", "/sys", "/proc"}
CRITICAL_PKGS = {"linux", "linux-lts", "linux-zen", "linux-firmware", "base",
                 "glibc", "systemd", "pacman", "bash", "filesystem", "coreutils",
                 "gcc-libs", "util-linux", "openssl", "mkinitcpio", "grub"}
DENY_RAW = [
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formatting a filesystem"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "writing raw data to a device"),
    (re.compile(r">\s*/dev/(sd|nvme|mmcblk|vd|hd)"), "writing to a disk device"),
    (re.compile(r"\b(wipefs|shred|blkdiscard|cryptsetup|sgdisk|sfdisk|fdisk|parted)\b"),
     "disk/partition tools"),
    (re.compile(r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"),
     "piping a download into a shell"),
]
WARN = [
    (re.compile(r"\brm\b"), "deletes files"),
    (re.compile(r"\bpacman\b.*\s-\w*R"), "removes packages"),
    (re.compile(r"\b(kill|pkill|killall)\b"), "stops processes"),
    (re.compile(r"\bsystemctl\b.*\b(stop|disable|mask)\b"), "stops or disables a service"),
    (re.compile(r"\b(reboot|poweroff|shutdown|halt)\b"), "restarts or powers off the machine"),
    (re.compile(r"\b(chmod|chown)\b"), "changes permissions or ownership"),
    (re.compile(r"\bdd\b"), "low-level copy"),
]
SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
ESCALATORS = {"sudo", "doas", "pkexec", "su"}
AUR_HELPERS = {"yay", "paru"}
TERMINALS = {"kitty": [], "foot": [], "alacritty": ["-e"], "ghostty": ["-e"],
             "wezterm": ["start", "--"], "konsole": ["-e"], "xterm": ["-e"]}

SYSTEM_PROMPT = (
    "You turn a request into ONE shell command for Arch Linux (bash, Hyprland, "
    "pacman). Reply with JSON only: {\"command\": \"<one line>\" or null, "
    "\"explanation\": \"<one short sentence>\"}. Use null when the request is "
    "not a task for a shell command. Prefer simple standard tools. Never use "
    "destructive commands.\n"
    "Examples:\n"
    "install firefox -> {\"command\": \"sudo pacman -S firefox\", \"explanation\": \"Installs Firefox.\"}\n"
    "how much disk space is free -> {\"command\": \"df -h /\", \"explanation\": \"Shows free disk space.\"}\n"
    "what's the capital of France -> {\"command\": null, \"explanation\": \"\"}")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _pacman_flags(tokens: list[str]) -> set[str]:
    longs = {"--sync": "S", "--remove": "R", "--upgrade": "U", "--refresh": "y",
             "--sysupgrade": "u", "--search": "s", "--info": "i"}
    flags: set[str] = set()
    for t in tokens[1:]:
        if t.startswith("--"):
            flags.add(longs.get(t, ""))
        elif t.startswith("-"):
            flags |= set(t[1:])
    return flags


class SystemCommands:
    def __init__(self, llm, terminal: str | None = None, timeout: float = 30,
                 max_output: int = 3000, interpret: bool = True):
        self.llm, self.terminal = llm, terminal
        self.timeout, self.max_output, self.interpret = timeout, max_output, interpret

    def problems(self) -> list[str]:
        return [] if shutil.which("bash") else ["bash not found"]

    # ---- 1. propose ------------------------------------------------------
    async def propose(self, request: str) -> Proposal | None:
        raw = await self.llm.complete(
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": request}],
            fmt="json", options={"temperature": 0.1})
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        cmd = data.get("command") if isinstance(data, dict) else None
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        expl = data.get("explanation")
        return self.evaluate(Proposal(cmd.strip(),
                                      expl.strip() if isinstance(expl, str) else ""))

    # ---- 2. validate -----------------------------------------------------
    def evaluate(self, p: Proposal) -> Proposal:
        cmd = re.sub(r"[ \t]+", " ", p.command.strip())
        p.command = cmd
        if "\n" in cmd or len(cmd) > 500:
            p.refused = "multi-line or very long commands aren't allowed"
            return p
        for pattern, why in DENY_RAW:
            if pattern.search(cmd):
                p.refused = f"blocked: {why}"
                return p

        segments = [_tokens(s) for s in SPLIT.split(cmd) if s]
        rebuilt = []
        for toks in segments:
            if not toks:
                continue
            first = toks[0]
            if first in ESCALATORS:
                p.needs_terminal = True
                toks = toks[1:] or toks   # judge the command after sudo
                first = toks[0]
            if first in AUR_HELPERS:
                p.needs_terminal = True   # prompts for sudo / interactive
            if first == "pacman" and not p.needs_terminal:
                f = _pacman_flags(toks)
                if f & {"R", "U"} or ("S" in f and (f & {"y", "u"} or not f & set("silgp"))):
                    p.needs_terminal = True
                    p.command = re.sub(r"^(\s*)pacman\b", r"\1sudo pacman", p.command, 1) \
                        if cmd.startswith("pacman") else p.command
            if first == "systemctl" and "--user" not in toks and \
                    {"start", "stop", "restart", "enable", "disable", "mask",
                     "unmask", "reload", "poweroff", "reboot"} & set(toks[1:]):
                p.needs_terminal = True   # system services need root
                if cmd.startswith("systemctl"):
                    p.command = "sudo " + p.command
            reason = self._deny_tokens(toks)
            if reason:
                p.refused = reason
                return p
            rebuilt.append(toks)

        reasons = [why for pattern, why in WARN if pattern.search(p.command)]
        if reasons:
            p.warning = "This " + " and ".join(dict.fromkeys(reasons)) + "."
        return p

    @staticmethod
    def _deny_tokens(toks: list[str]) -> str | None:
        name, args = toks[0], toks[1:]
        low = [a.lower() for a in args]
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        targets = [a for a in low if not a.startswith("-")]
        recursive = ("r" in flags.lower()) or "--recursive" in low
        if name == "rm":
            if "--no-preserve-root" in low:
                return "blocked: --no-preserve-root"
            if recursive and any(t.rstrip("/") in PROTECTED or t in PROTECTED for t in targets):
                return "blocked: recursive delete of a protected or broad path"
        if name in ("chmod", "chown", "chgrp") and recursive and \
                any(t in PROTECTED for t in targets[1:] or targets):
            return "blocked: recursive permission change on a protected path"
        if name == "pacman" and any(a.startswith("-R") for a in args):
            if any(t in CRITICAL_PKGS for t in targets) or "-rdd" in low or "-rnsdd" in low:
                return "blocked: removing a critical system package"
        return None

    # ---- 3a. run directly ---------------------------------------------------
    async def run(self, p: Proposal) -> Result:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", p.command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=os.path.expanduser("~"), start_new_session=True)
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = await proc.communicate()
        text = out.decode("utf-8", "replace").strip()
        truncated = len(text) > self.max_output
        if truncated:
            text = text[: self.max_output] + "\n... (output truncated)"
        return Result(proc.returncode if proc.returncode is not None else -1,
                      text, truncated, timed_out)

    # ---- 3b. run in a terminal (root / interactive) ------------------------
    def _terminal(self) -> tuple[str, list[str]] | None:
        names = [self.terminal] if self.terminal else list(TERMINALS)
        for name in names:
            if name and shutil.which(name):
                return name, TERMINALS.get(name, ["-e"])
        return None

    async def run_in_terminal(self, p: Proposal) -> str:
        found = self._terminal()
        if found:
            name, args = found
            wrapped = f"{p.command}; echo; read -rp 'Finished. Press Enter to close...'"
            subprocess.Popen([shutil.which(name), *args, "bash", "-c", wrapped],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return f"Opened {name} to run it. Enter your password there if asked."
        for clip in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(clip[0]):
                subprocess.run(clip, input=p.command.encode(), check=False)
                return "No terminal found. The command is on your clipboard; paste it into a terminal."
        return "No terminal found. Run this yourself:\n" + p.command

    # ---- 4. interpret -----------------------------------------------------
    @staticmethod
    def interpret_messages(system_prompt: str, request: str, p: Proposal, r: Result):
        status = "it timed out" if r.timed_out else f"exit code {r.returncode}"
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content":
                f'I asked: "{request}"\nYou ran: {p.command}\nResult: {status}\n'
                f"Output:\n{r.output[:1500] or '(no output)'}\n\n"
                "In 1-3 short sentences, say what this shows or whether it worked."},
        ]
