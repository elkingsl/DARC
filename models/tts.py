"""Piper text-to-speech: synthesize to a temp wav, then play it."""
import asyncio
import os
import re
import shutil
import tempfile


class TTSError(Exception):
    pass


PLAYERS = (("pw-play", []), ("paplay", []), ("aplay", ["-q"]), ("play", ["-q"]))


def clean_for_speech(text: str, max_chars: int = 400) -> str:
    """Strip markdown/code so the voice doesn't read symbols aloud."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", " link ", text)
    text = re.sub(r"[*_#>~|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:end + 1] if end > 40 else cut
    return text


class PiperTTS:
    def __init__(self, piper_bin: str, voice: str, max_chars: int = 400):
        self.piper = os.path.expanduser(piper_bin)
        self.voice = os.path.expanduser(voice)
        self.max_chars = max_chars
        self._player_proc = None

    def _player(self, wav: str):
        for name, extra in PLAYERS:
            exe = shutil.which(name)
            if exe:
                return [exe, *extra, wav]
        return None

    def problems(self) -> list[str]:
        out = []
        if not os.access(self.piper, os.X_OK):
            out.append(f"piper binary not found: {self.piper}")
        if not os.path.exists(self.voice):
            out.append(f"piper voice not found: {self.voice}")
        if not any(shutil.which(n) for n, _ in PLAYERS):
            out.append("no audio player (need pw-play, paplay, aplay or sox)")
        return out

    async def speak(self, text: str) -> None:
        text = clean_for_speech(text, self.max_chars)
        if not text:
            return
        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="darc-tts-")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.piper, "-m", self.voice, "-f", wav,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.communicate(text.encode()), 60)
            except asyncio.TimeoutError:
                proc.kill()
                raise TTSError("piper timed out")
            if proc.returncode != 0:
                raise TTSError(f"piper exited with {proc.returncode}")
            self._player_proc = await asyncio.create_subprocess_exec(
                *self._player(wav),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await self._player_proc.wait()
        finally:
            self._player_proc = None
            try:
                os.unlink(wav)
            except FileNotFoundError:
                pass

    def stop(self) -> None:
        """Cut speech short (playback only)."""
        p = self._player_proc
        if p and p.returncode is None:
            p.terminate()
