"""Cheap, instant intent routing (no LLM call).

Kinds: chat | vision | window | browser | open | web | command
classify(text, skip) can be re-run with the kinds that already declined, so a
wrong guess (e.g. "open the config file" is not an app) just falls through to
the next best route and finally to plain chat. Nothing ever runs without the
user's confirmation where it matters (commands, typing, buy/delete clicks)."""
import re
from dataclasses import dataclass


@dataclass
class Route:
    kind: str = "chat"
    region: bool = False        # vision: let the user select a screen region
    arg: str = ""               # open/web: the target or query
    data: dict | None = None    # window/browser/open: parsed details


I = re.IGNORECASE
VISION = [re.compile(p) for p in (
    r"\bwhat\s+(am\s+i|are\s+we)\s+looking\s+at\b",
    r"\bwhat('?s|\s+is)\s+(on|in)\s+(my|the)\s+(screen|display|monitor)\b",
    r"\b(look(\s+at)?|read|describe|check|see|analy[sz]e|scan|explain)\b.{0,25}"
    r"\b(my|the|this)\s+(screen|display|monitor)\b",
    r"\b(on|at)\s+(my|the)\s+screen\b",
    r"\bscreenshot\b",
)]
REGION = re.compile(r"\b(select|selection|region|area|part of)\b")

WINDOW = [
    (re.compile(r"^(go|switch|jump)\s+to\s+workspace\s+(\d+)"), "workspace"),
    (re.compile(r"\bmove\b.*\bto\s+workspace\s+(\d+)"), "movetoworkspace"),
    (re.compile(r"\bclose\b.*\b(this|the|active|current)\s+window\b"), "close"),
    (re.compile(r"\b(toggle\s+)?full\s?screen\b"), "fullscreen"),
]

TAB_OPS = [
    (re.compile(r"\b(new|open a new)\s+tab\b"), "new_tab"),
    (re.compile(r"\bclose\b.*\btab\b"), "close_tab"),
    (re.compile(r"\b(next|right)\s+tab\b|\bswitch\s+tab\b"), "next_tab"),
    (re.compile(r"\b(previous|prev|last|left)\s+tab\b"), "prev_tab"),
    (re.compile(r"\bgo\s+back\b|^back$"), "back"),
    (re.compile(r"\bgo\s+forward\b"), "forward"),
    (re.compile(r"\b(reload|refresh)\b.*\bpage\b|^(reload|refresh)$"), "reload"),
    (re.compile(r"\bscroll\s+(down|up)\b"), "scroll"),
    (re.compile(r"\b(read|summari[sz]e|tl;?dr)\b.*\b(this|the|current)\s+(page|tab|article)\b"), "read"),
]
DOMAIN_TOKEN = re.compile(r"\b[a-z0-9-]+\.(com|org|net|io|dev|app|co|gg|tv|me|ai|edu|gov|info|xyz|fr|dz|uk|de)\b")
BROWSER_VERBS = re.compile(r"\b(find|search|click|check|look|log\s?in|sign\s?in|read|download|add|get|see|what|show|fill|open|price|top|latest)\b")
GOTO = re.compile(r"^(go to|navigate to|browse to|visit)\s+(?P<site>\S+)(?P<rest>\s+.+)?$")

SITE_SEARCH = [
    re.compile(r"^(?:search|look up|find)\s+(?P<site>[a-z ]+?)\s+for\s+(?P<q>.+)$"),
    re.compile(r"^(?:search|look up|find)\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in)\s+(?P<site>[a-z ]+)$"),
]
OPEN_VERB = re.compile(r"^(open|launch|start)\s+(?P<t>.+)$")
WEB = [re.compile(p) for p in (
    r"^(?:search|google|look\s*up|find)\s+(?:the\s+)?(?:web|internet|online)\s+(?:for\s+)?(?P<q>.+)$",
    r"^(?:google|look\s*up|duckduckgo)\s+(?P<q>.+)$",
    r"^search\s+(?:for\s+)?(?P<q>.+)$",
)]
WEB_FRESH = re.compile(r"\b(latest|newest|news|release notes|changelog|who won|score of|weather in|price of)\b")
URL = re.compile(r"https?://\S+")

# --- shell commands (Phase 4) ---
STRONG = {"install", "uninstall", "reboot", "shutdown", "poweroff", "kill",
          "mount", "unmount", "run", "execute", "pacman", "systemctl", "hyprctl"}
WEAK = {"remove", "update", "upgrade", "restart", "show", "list", "check",
        "find", "open", "start", "stop", "create", "delete", "copy", "move",
        "rename", "set", "turn", "mute", "unmute", "increase", "decrease",
        "lower", "raise", "enable", "disable", "connect", "disconnect",
        "clone", "launch", "what", "which", "is", "are"}
NOUNS = re.compile(
    r"\b(packages?|system|disk|space|memory|ram|cpu|battery|ip|network|wi-?fi|"
    r"bluetooth|volume|brightness|files?|folders?|director(y|ies)|services?|"
    r"process(es)?|kernel|uptime|workspace|windows?|audio|sound|mic(rophone)?|"
    r"pacman|systemctl|hyprctl|git|docker|ssh|ports?|logs?|temperature|usb|"
    r"drive|partition|hostname|timezone|installed|terminal)\b")
INFO = [re.compile(p) for p in (
    r"\bhow\s+(much|many)\b.*\b(disk|space|memory|ram|storage|cpu|files|packages|processes)\b",
    r"\bwhat('?s|\s+is|\s+are)\b.*\b(my|the)\b.*\b(ip|kernel|uptime|battery|hostname|disk|memory|ram|cpu|temperature|time|date|volume|brightness)\b",
    r"\bwhat\s+(time|day)\s+is\s+it\b",
    r"\b(is|are)\b.*\b(installed|connected)\b",
)]
FILLER = re.compile(r"^(hey|ok|okay)?[\s,]*(darc|jarvis)?[\s,.!]*"
                    r"((please|can you|could you|would you|will you)\s+)*")


class Router:
    def classify(self, text: str, skip=frozenset()) -> Route:
        t = FILLER.sub("", text.lower().strip()).strip().rstrip("?.!")
        if not t:
            return Route()
        words = t.split()
        first = words[0].strip(",.!?")

        if "vision" not in skip and any(p.search(t) for p in VISION):
            return Route("vision", region=bool(REGION.search(t)))

        if "window" not in skip:
            for pat, op in WINDOW:
                m = pat.search(t)
                if m:
                    data = {"op": op}
                    if op in ("workspace", "movetoworkspace"):
                        data["n"] = int(m.groups()[-1])
                    return Route("window", data=data)

        if "browser" not in skip:
            r = self._browser(t)
            if r:
                return r

        if "open" not in skip:
            r = self._open(t)
            if r:
                return r

        if "web" not in skip:
            r = self._web(t)
            if r:
                return r

        if "command" not in skip:
            if first in STRONG or any(p.search(t) for p in INFO) \
                    or (first in WEAK and NOUNS.search(t)):
                return Route("command")
        return Route()

    @staticmethod
    def _browser(t: str) -> Route | None:
        for pat, op in TAB_OPS:
            if pat.search(t):
                data = {"op": op}
                if op == "scroll":
                    data["direction"] = pat.search(t).group(1)
                m = URL.search(t) or DOMAIN_TOKEN.search(t)
                if op == "new_tab" and m:
                    data["url"] = m.group(0)
                return Route("browser", data=data)
        if re.search(r"\b(in|using|with|on)\s+(the\s+)?(browser|chrome)\b", t) \
                and BROWSER_VERBS.search(t):
            return Route("browser", data={"op": "task", "goal": t})
        m = GOTO.match(t)
        if m and m.group("rest") and re.match(r"\s*(and|then|,)\b", m.group("rest")):
            return Route("browser", data={"op": "task", "goal": t})
        if DOMAIN_TOKEN.search(t) and BROWSER_VERBS.search(t) \
                and not t.split()[0] in ("open", "go", "visit", "navigate", "browse", "summarize", "summarise", "read"):
            return Route("browser", data={"op": "task", "goal": t})
        return None

    @staticmethod
    def _open(t: str) -> Route | None:
        for pat in SITE_SEARCH:
            m = pat.match(t)
            if m:
                return Route("open", data={"search_site": m.group("site").strip(),
                                           "q": m.group("q").strip()})
        m = GOTO.match(t)
        if m and not m.group("rest"):
            return Route("open", arg=m.group("site"))
        m = OPEN_VERB.match(t)
        if m:
            return Route("open", arg=m.group("t").strip())
        return None

    @staticmethod
    def _web(t: str) -> Route | None:
        u = URL.search(t)
        if u and re.match(r"^(summari[sz]e|read|tl;?dr|what('?s| does| is)|explain|tell me about)\b", t):
            return Route("web", arg=u.group(0), data={"url": u.group(0), "question": t})
        for pat in WEB:
            m = pat.match(t)
            if m:
                return Route("web", arg=m.group("q").strip())
        if WEB_FRESH.search(t) and re.match(r"^(what|who|when|where|how|is|are|any|tell|show|did|does)\b", t):
            return Route("web", arg=t)
        return None
