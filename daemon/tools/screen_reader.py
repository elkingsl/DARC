"""Screen capture (grim / maim) + local vision model (moondream via Ollama).
Screenshots stay in memory and only ever go to the local Ollama server."""
import asyncio
import base64
import io
import os
import shutil


class ScreenError(Exception):
    pass


class ScreenReader:
    def __init__(self, llm, model="moondream", keep_alive="1m",
                 max_side=1024, refine=True):
        self.llm, self.model, self.keep_alive = llm, model, keep_alive
        self.max_side, self.refine = max_side, refine

    @staticmethod
    def wayland() -> bool:
        return bool(os.environ.get("WAYLAND_DISPLAY"))

    def problems(self) -> list[str]:
        need = ["grim", "slurp"] if self.wayland() else ["maim"]
        return [f"{t} not installed (sudo pacman -S {t})"
                for t in need if not shutil.which(t)]

    async def _run(self, cmd: list[str], timeout: float) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise ScreenError(f"{cmd[0]} is not installed")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ScreenError(f"{cmd[0]} timed out")
        if proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip() or "cancelled"
            raise ScreenError(f"{cmd[0]}: {msg}")
        return out

    async def capture(self, region: bool = False) -> bytes:
        if self.wayland():
            cmd = ["grim", "-t", "png", "-"]
            if region:  # user drags a rectangle with slurp
                geo = (await self._run(["slurp"], 30)).decode().strip()
                if not geo:
                    raise ScreenError("no region selected")
                cmd = ["grim", "-g", geo, "-t", "png", "-"]
        else:
            cmd = ["maim", "-s"] if region else ["maim"]
        png = await self._run(cmd, 15)
        if not png:
            raise ScreenError("screenshot was empty")
        return await asyncio.to_thread(self._shrink, png)

    def _shrink(self, png: bytes) -> bytes:
        """Downscale + JPEG: the vision model works at ~378px anyway."""
        try:
            from PIL import Image
        except ImportError:
            return png
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im.thumbnail((self.max_side, self.max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def describe(self, image: bytes, question: str):
        """Async iterator of the vision model's answer."""
        msgs = [{"role": "user", "content": question,
                 "images": [base64.b64encode(image).decode()]}]
        return self.llm.stream_chat(msgs, model=self.model,
                                    keep_alive=self.keep_alive)

    @staticmethod
    def refine_messages(system_prompt: str, question: str, description: str):
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content":
                f'I asked: "{question}"\nA vision model looked at my screen '
                f"and said:\n{description}\n\nAnswer my question in 1-3 short "
                "sentences using only that. If it is unclear, say so."},
        ]
