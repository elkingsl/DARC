"""whisper.cpp speech-to-text via the whisper-cli binary."""
import asyncio
import os
import re
import tempfile
import wave


class STTError(Exception):
    pass


class WhisperSTT:
    def __init__(self, binary: str, model: str, threads: int = 2,
                 language: str = "en", rate: int = 16000):
        self.binary = os.path.expanduser(binary)
        self.model = os.path.expanduser(model)
        self.threads, self.language, self.rate = threads, language, rate

    def problems(self) -> list[str]:
        out = []
        if not os.access(self.binary, os.X_OK):
            out.append(f"whisper binary not found: {self.binary}")
        if not os.path.exists(self.model):
            out.append(f"whisper model not found: {self.model}")
        return out

    async def transcribe(self, samples) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="darc-")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.rate)
                w.writeframes(samples.tobytes())
            proc = await asyncio.create_subprocess_exec(
                self.binary, "-m", self.model, "-f", path,
                "-t", str(self.threads), "-l", self.language, "-nt", "-np",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 60)
            except asyncio.TimeoutError:
                proc.kill()
                raise STTError("whisper timed out")
            if proc.returncode != 0:
                raise STTError(f"whisper exited with {proc.returncode}")
            return self.clean(out.decode("utf-8", "replace"))
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @staticmethod
    def clean(text: str) -> str:
        """Drop whisper's non-speech markers like [BLANK_AUDIO] or (music)."""
        text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)
        return re.sub(r"\s+", " ", text).strip()
