"""Voice pipeline: wake word / hotkey -> record -> whisper.cpp -> orchestrator
-> (reply) -> Piper. Every piece degrades gracefully: missing models or no
microphone just disable voice; text chat keeps working."""
import asyncio

from daemon.orchestrator import State
from models.mic import Microphone
from models.stt import WhisperSTT
from models.tts import PiperTTS


class VoicePipeline:
    def __init__(self, orch, cfg: dict):
        self.orch = orch
        self.cfg = cfg
        self.mic: Microphone | None = None
        self.stt: WhisperSTT | None = None
        self.tts: PiperTTS | None = None
        self.can_listen = False
        self.speak_mode = cfg.get("tts", {}).get("speak", "voice_only")
        self._busy = False
        self._tasks: set[asyncio.Task] = set()

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> list[str]:
        notes: list[str] = []
        a = self.cfg.get("audio", {})
        s = self.cfg.get("stt", {})
        t = self.cfg.get("tts", {})

        if t.get("enabled", True) and self.speak_mode != "never":
            tts = PiperTTS(t["piper_bin"], t["voice"], t.get("max_chars", 400))
            problems = tts.problems()
            if problems:
                notes += [f"voice output off: {p}" for p in problems]
            else:
                self.tts = tts
                self.orch.reply_hook = self.on_reply

        stt = WhisperSTT(s["binary"], s["model"], s.get("threads", 2),
                         s.get("language", "en"), a.get("sample_rate", 16000))
        problems = stt.problems()
        if problems:
            notes += [f"voice input off: {p}" for p in problems]
        else:
            try:
                import sounddevice  # noqa: F401
                self.stt = stt
            except Exception as e:
                notes.append(f"voice input off: sounddevice unavailable ({e})")
        if not self.stt:
            return notes

        detector = None
        if a.get("wake_word"):
            try:
                from models.wake_word import WakeWord
                detector = await asyncio.to_thread(
                    WakeWord, a["wake_word"], a.get("wake_threshold", 0.5))
            except Exception as e:
                notes.append(f"wake word off ({e}); use `darcctl.py listen`")

        self.mic = Microphone(
            asyncio.get_running_loop(),
            rate=a.get("sample_rate", 16000), device=a.get("input_device"),
            detector=detector, on_wake=self._on_wake, on_error=self._on_mic_error,
            vad_threshold=a.get("vad_threshold", 500),
            silence_ms=a.get("silence_ms", 900),
            max_record_s=a.get("max_record_s", 12),
            start_timeout_s=a.get("start_timeout_s", 5))
        self.mic.start()
        self.can_listen = True
        self.orch.subscribe(self._on_event)
        if self.orch.state == State.IDLE:
            self.mic.arm()
        wake = f"wake word '{a['wake_word']}'" if detector else "hotkey only"
        notes.append(f"voice input ready ({wake})")
        if self.tts:
            notes.append(f"voice output ready (speak: {self.speak_mode})")
        return notes

    def stop(self) -> None:
        if self.mic:
            self.mic.stop()
        if self.tts:
            self.tts.stop()

    # ---- events ---------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        if ev.get("type") != "state" or not self.mic:
            return
        if ev["state"] == State.IDLE.value:
            self.mic.arm()      # listen for the wake word only when idle,
        else:
            self.mic.disarm()   # so our own speech never triggers it

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_wake(self, job_future) -> None:  # runs on the asyncio thread
        self._spawn(self.start_session(job_future))

    def _on_mic_error(self, err: Exception) -> None:
        self.can_listen = False
        print(f"[voice] microphone error: {err}")
        self._spawn(self._recover(f"microphone error: {err}"))

    async def _recover(self, message: str) -> None:
        await self.orch.emit({"type": "error", "id": 0, "message": message})
        if self.orch.state != State.IDLE:
            await self.orch.mark(State.IDLE)

    # ---- one voice interaction -------------------------------------------
    async def start_session(self, job_future=None) -> None:
        if self._busy or not self.mic:
            if job_future:
                self.mic.cancel_recording()
            return
        self._busy = True
        try:
            if not await self.orch.begin_listening():  # only from IDLE
                self.mic.cancel_recording()
                return
            audio = await asyncio.wrap_future(job_future or self.mic.start_recording())
            if audio is None:  # silence, timeout or cancelled
                await self.orch.mark(State.IDLE)
                return
            await self.orch.mark(State.PROCESSING)
            text = await self.stt.transcribe(audio)
            print(f"[heard] {text!r}")
            if not text:
                await self.orch.mark(State.IDLE)
                return
            await self.orch.emit({"type": "transcript", "text": text})
            self.orch.submit(text, source="voice")
        except Exception as e:
            await self.orch.emit({"type": "error", "id": 0, "message": f"voice: {e}"})
            await self.orch.mark(State.IDLE)
        finally:
            self._busy = False

    async def cancel(self) -> None:
        if self.tts:
            self.tts.stop()
        if self.mic and self._busy:
            self.mic.cancel_recording()  # the running session winds itself down
        else:
            await self.orch.cancel_listening()

    async def on_reply(self, text: str, source: str) -> None:
        if not self.tts or self.speak_mode == "never":
            return
        if self.speak_mode == "voice_only" and source != "voice":
            return
        await self.tts.speak(text)
