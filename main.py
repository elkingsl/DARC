"""Phase 1 entry point: CLI prompt -> orchestrator -> Ollama -> streamed reply."""
import asyncio
import sys
from pathlib import Path

import yaml

from daemon.orchestrator import Orchestrator
from models.llm import LLMError, OllamaLLM

CONFIG = Path(__file__).with_name("config.yaml")


def printer(event: dict) -> None:
    t = event["type"]
    if t == "token":
        print(event["text"], end="", flush=True)
    elif t == "done":
        print("\n")
    elif t == "error":
        print(f"\n[error] {event['message']}\n")
    elif t == "state":
        print(f"\033[2m[{event['state']}]\033[0m", end=" ", flush=True)


async def main() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    llm_cfg, d_cfg = cfg["llm"], cfg["daemon"]
    llm = OllamaLLM(llm_cfg["host"], llm_cfg["model"],
                    keep_alive=llm_cfg.get("keep_alive", "5m"),
                    options=llm_cfg.get("options"),
                    timeout=llm_cfg.get("timeout", 120))
    darc = Orchestrator(llm, d_cfg["system_prompt"],
                        d_cfg.get("max_history_turns", 6))
    darc.subscribe(printer)

    try:
        await darc.start()
    except LLMError as e:
        sys.exit(f"[startup failed] {e}")

    print("DARC ready. /clear resets context, /quit exits.\n")
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/clear":
                darc.clear_history()
                print("context cleared")
                continue
            darc.submit(line)
            await darc.join()
    except KeyboardInterrupt:
        pass
    finally:
        await darc.stop()


if __name__ == "__main__":
    asyncio.run(main())
