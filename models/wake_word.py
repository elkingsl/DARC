"""openwakeword wrapper. Works with both the 0.4.x API (models bundled in the
wheel, wakeword_model_paths=) and 0.5+/0.6 (download_models + wakeword_models=)."""
import numpy as np


class WakeWord:
    def __init__(self, name: str = "hey_jarvis", threshold: float = 0.5):
        self.name = name
        self.threshold = threshold
        self.model = self._load(name)

    @staticmethod
    def _load(name: str):
        import openwakeword
        from openwakeword.model import Model

        utils = getattr(openwakeword, "utils", None)
        if utils is not None and hasattr(utils, "download_models"):
            try:  # newer versions fetch models on first use (small, one-time)
                utils.download_models([name])
            except Exception as e:
                print(f"[wake] model download failed: {e}")

        attempts = [
            lambda: Model(wakeword_models=[name], inference_framework="onnx"),
            lambda: Model(wakeword_models=[name]),
            lambda: Model(wakeword_model_paths=[
                p for p in openwakeword.get_pretrained_model_paths() if name in p]),
        ]
        last = None
        for make in attempts:
            try:
                return make()
            except Exception as e:  # API differs between versions
                last = e
        raise RuntimeError(f"could not load wake word '{name}': {last}")

    def detect(self, chunk: np.ndarray) -> bool:
        """chunk: int16 samples, 1280 (80 ms @ 16 kHz)."""
        scores = self.model.predict(chunk)
        return bool(scores) and max(scores.values()) >= self.threshold

    def reset(self) -> None:
        try:
            self.model.reset()
        except Exception:
            pass
