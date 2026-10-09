"""One thread owns the microphone: wake-word listening + utterance recording.

Modes:  idle  (audio read and discarded)
        wake  (feed the wake-word detector)
        record (energy-based VAD; ends after trailing silence)
"""
import collections
import concurrent.futures
import threading

import numpy as np

CHUNK = 1280  # 80 ms @ 16 kHz: the frame size openwakeword expects
CHUNK_MS = 80


class _Job:
    def __init__(self):
        self.future = concurrent.futures.Future()  # thread-safe result slot
        self.pre = collections.deque(maxlen=4)     # short pre-roll
        self.frames: list[np.ndarray] = []
        self.started = False
        self.loud = 0
        self.silent = 0
        self.chunks = 0


class Microphone(threading.Thread):
    def __init__(self, loop, *, rate=16000, device=None, detector=None,
                 on_wake=None, on_error=None, vad_threshold=500,
                 silence_ms=900, max_record_s=12, start_timeout_s=5,
                 stream_factory=None):
        super().__init__(daemon=True, name="darc-mic")
        self._loop, self._rate, self._device = loop, rate, device
        self._detector, self._on_wake, self._on_error = detector, on_wake, on_error
        self._vad = vad_threshold
        self._silence_chunks = max(1, silence_ms // CHUNK_MS)
        self._max_chunks = int(max_record_s * 1000 / CHUNK_MS)
        self._timeout_chunks = int(start_timeout_s * 1000 / CHUNK_MS)
        self._factory = stream_factory or self._default_factory
        self._lock = threading.Lock()
        self._mode = "idle"
        self._job: _Job | None = None
        self._halt = threading.Event()

    def _default_factory(self):
        import sounddevice as sd
        return sd.InputStream(samplerate=self._rate, channels=1, dtype="int16",
                              blocksize=CHUNK, device=self._device)

    # ---- control (called from the asyncio thread) -------------------------
    def arm(self) -> None:
        """Listen for the wake word (no-op while recording)."""
        with self._lock:
            if self._detector and self._mode == "idle":
                self._mode = "wake"

    def disarm(self) -> None:
        with self._lock:
            if self._mode == "wake":
                self._mode = "idle"

    def start_recording(self) -> concurrent.futures.Future:
        """Returns a Future -> int16 ndarray, or None if nothing was said."""
        with self._lock:
            job = _Job()
            self._job, self._mode = job, "record"
            return job.future

    def cancel_recording(self) -> None:
        with self._lock:
            job = self._job
        if job:
            self._finish(job, None)

    def stop(self) -> None:
        self._halt.set()

    # ---- thread ---------------------------------------------------------
    def run(self):
        try:
            with self._factory() as stream:
                while not self._halt.is_set():
                    data = stream.read(CHUNK)[0]
                    self._process(np.asarray(data).reshape(-1).astype(np.int16))
        except Exception as e:
            job = self._job
            if job:
                self._finish(job, None)
            if self._on_error:
                self._loop.call_soon_threadsafe(self._on_error, e)

    def _process(self, chunk: np.ndarray) -> None:
        with self._lock:
            mode, job = self._mode, self._job
        if mode == "wake" and self._detector:
            if self._detector.detect(chunk):
                with self._lock:
                    if self._mode != "wake":
                        return
                    job = self._job = _Job()
                    self._mode = "record"  # start recording with no gap
                self._detector.reset()
                self._loop.call_soon_threadsafe(self._on_wake, job.future)
        elif mode == "record" and job:
            self._record_step(job, chunk)

    def _record_step(self, job: _Job, chunk: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        job.chunks += 1
        if rms >= self._vad:
            job.loud += 1
            job.silent = 0
            if not job.started:
                job.started = True
                job.frames = list(job.pre)
        elif job.started:
            job.silent += 1
        if job.started:
            job.frames.append(chunk)
        else:
            job.pre.append(chunk)

        if job.started and job.silent >= self._silence_chunks:
            self._finish(job, self._audio(job))
        elif not job.started and job.chunks >= self._timeout_chunks:
            self._finish(job, None)
        elif job.chunks >= self._max_chunks:
            self._finish(job, self._audio(job))

    @staticmethod
    def _audio(job: _Job):
        if job.loud < 2 or not job.frames:  # a click or pop, not speech
            return None
        return np.concatenate(job.frames)

    def _finish(self, job: _Job, audio) -> None:
        with self._lock:
            if self._job is job:
                self._job = None
                self._mode = "idle"
        if not job.future.done():
            job.future.set_result(audio)
