"""在独立的 Python 3.12 环境中复用本地炫神模型。"""
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
import struct
import subprocess
import sys
from threading import Lock, Thread

_LENGTH = struct.Struct("<I")
_RUNTIME = Path(__file__).resolve().parents[2] / ".runtime/voice-xuanshen"


class XuanShenVoice:
    def __init__(self):
        self._lock = Lock()
        self._responses = Queue()
        self._process = subprocess.Popen(
            [str(_RUNTIME / "venv/Scripts/python.exe"), "-u", "-X", "utf8", __file__],
            cwd=_RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        Thread(target=self._read_responses, name="XuanShenVoice", daemon=True).start()

    @staticmethod
    def available():
        return (_RUNTIME / "venv/Scripts/python.exe").is_file() and (_RUNTIME / "speakers/XuanShen/gpt.ckpt").is_file()

    def _read_responses(self):
        with self._process.stdout as wire:
            while header := wire.read(_LENGTH.size):
                size, = _LENGTH.unpack(header)
                self._responses.put(wire.read(size))
        self._responses.put(b"")

    def synthesize(self, text):
        with self._lock:
            try:
                self._process.stdin.write((text + "\n").encode("utf-8"))
                self._process.stdin.flush()
                audio = self._responses.get(timeout=50)
            except Empty:
                self.close()
                raise TimeoutError("炫神语音合成超时") from None
            if not audio:
                raise OSError("炫神语音合成没有返回音频")
            return audio

    def close(self):
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait()
        self._process.stdin.close()


def _worker():
    import os
    import time

    root = Path.cwd()
    sys.path[:0] = [str(root / "deps-zh"), str(root / "parrots-src")]
    os.environ["NLTK_DATA"] = str(root / "nltk-data")
    os.environ["NUMBA_CACHE_DIR"] = str(root / "numba-cache")
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    started = time.perf_counter()
    import soundfile
    from scipy.signal import resample_poly
    import torch
    from parrots import TextToSpeech, set_log_level

    set_log_level("WARNING")
    model = TextToSpeech(
        bert_model_path=str(root / "parrots-chinese-roberta-wwm-ext-large"),
        hubert_model_path=str(root / "parrots-chinese-hubert-base"),
        speaker_model_path=str(root / "speakers"), speaker_name="XuanShen",
        device="cuda", half=True, ensure_nltk=False,
    )
    with torch.inference_mode():
        model.predict("大家好。", text_language="zh")
    print(f"炫神音色已预热：{time.perf_counter() - started:.2f} 秒", file=sys.stderr)
    for line in sys.stdin.buffer:
        with torch.inference_mode():
            samples = model.predict(line.decode("utf-8").strip(), text_language="zh")
        wav = BytesIO()
        samples = resample_poly(samples, 16000, model.sampling_rate)
        soundfile.write(wav, samples, 16000, format="WAV", subtype="PCM_16")
        data = wav.getbuffer()
        wire.write(_LENGTH.pack(len(data)))
        wire.write(data)
        wire.flush()


if __name__ == "__main__":
    _worker()
